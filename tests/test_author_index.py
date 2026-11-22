"""The author index (#117b): `authors` + `item_authors`, derived from `items.authors`.

The string stays the record; these tables are an index over it, written only by
`app/services/author_index.py` through the item write funnel and the boot rebuild.
"""

import re
import sqlite3
import logging

from app.database import _run_migrations
from app.services import authors as authors_svc
from app.services.item_write import (
    insert_item, trash_item, update_item_fields, update_items_fields, was_restored,
)
from tests.conftest import _insert_item
from tests.lineage import captured_fork_database

_NEW_DDL = re.compile(
    r"CREATE TABLE IF NOT EXISTS (authors|item_authors) \(.*?\n\);"
    r"|CREATE INDEX IF NOT EXISTS idx_item_authors_author [^;]*;",
    re.S,
)


def _objects(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('authors', 'item_authors', 'idx_item_authors_author')")
    }


def _pre_change_db(path):
    """A database as the release before #117b left it (G98): every numbered
    migration applied and recorded, and MIGRATION_TABLES *minus* the author DDL.
    Built from the pre-change SQL, never the current MIGRATION_TABLES, so the
    upgrade below has to create the tables itself."""
    # D1: the historical fork has neither author DDL nor bridge/provenance rows.
    conn = captured_fork_database(path)
    assert _objects(conn) == set()
    return conn


def test_fresh_database_has_author_tables(db):
    assert _objects(db) == {"authors", "item_authors", "idx_item_authors_author"}


def test_upgraded_database_gains_author_tables(tmp_path, monkeypatch, caplog):
    conn = _pre_change_db(tmp_path / "legacy.db")
    assert _objects(conn) == set()
    conn.execute(
        "INSERT INTO items (title, authors, source) VALUES (?, ?, 'test')",
        ("The Three-Body Problem", "Cixin Liu, Ken Liu - translator"))
    conn.commit()
    _run_migrations(conn)
    conn.commit()
    assert _objects(conn) == {"authors", "item_authors", "idx_item_authors_author"}
    assert conn.execute("SELECT COUNT(*) FROM item_authors").fetchone()[0] == 0
    conn.close()
    # The derived rebuild moved after the atomic schema transaction. Drive the
    # genuine boot owner, not a test-side call to the implementation under test.
    from app.database import init_db

    monkeypatch.setattr("app.database.DATABASE_PATH", tmp_path / "legacy.db")
    monkeypatch.setattr("app.database.COVERS_DIR", tmp_path / "covers")
    with caplog.at_level(logging.INFO, logger="app.database"):
        init_db()
    assert "Built author index: 1 items, 2 authors" in [r.getMessage() for r in caplog.records]
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    assert conn.execute("SELECT COUNT(*) FROM item_authors").fetchone()[0] == 2
    conn.close()


def test_purge_cascades_item_authors(db):
    from app.services.trash import purge_item

    item_id = _insert_item(db, authors="Kent Beck")
    author_id = db.execute(
        "INSERT INTO authors (name, name_key) VALUES ('Kent Beck', 'kent beck')"
    ).lastrowid
    db.execute(
        "INSERT INTO item_authors (item_id, author_id, position) VALUES (?, ?, 0)",
        (item_id, author_id),
    )
    assert trash_item(db, item_id)
    assert purge_item(db, item_id)
    assert db.execute(
        "SELECT COUNT(*) FROM item_authors WHERE item_id = ?", (item_id,)
    ).fetchone()[0] == 0


def test_author_ids_are_never_reused(db):
    """AUTOINCREMENT: a garbage-collected author's id is not handed to the
    next author, so a kept Browse URL cannot point at someone else."""
    first = db.execute(
        "INSERT INTO authors (name, name_key) VALUES ('A', 'a')").lastrowid
    db.execute("DELETE FROM authors WHERE id = ?", (first,))
    second = db.execute(
        "INSERT INTO authors (name, name_key) VALUES ('B', 'b')").lastrowid
    assert second > first


# -- the write funnel keeps the index in step (T3) ---------------------------

def _index(db, item_id):
    return [
        (r["name"], r["role"], r["position"]) for r in db.execute(
            "SELECT a.name, ia.role, ia.position FROM item_authors ia "
            "JOIN authors a ON a.id = ia.author_id WHERE ia.item_id = ? "
            "ORDER BY ia.position", (item_id,))
    ]


def _expected(authors):
    return [(p.name, p.role, p.position) for p in authors_svc.parse(authors)]


def _author_ids(db, item_id):
    return [r[0] for r in db.execute(
        "SELECT author_id FROM item_authors WHERE item_id = ? ORDER BY position",
        (item_id,))]


ROLES = "Cixin Liu, Ken Liu - translator"


def test_insert_indexes_the_string(db):
    item_id = insert_item(db, title="The Three-Body Problem", authors=ROLES)
    assert _index(db, item_id) == _expected(ROLES) == [
        ("Cixin Liu", None, 0), ("Ken Liu", "translator", 1)]


def test_insert_without_authors_writes_no_index(db):
    item_id = insert_item(db, title="Anonymous")
    assert _index(db, item_id) == []


def test_update_reindexes_in_order(db):
    item_id = insert_item(db, title="Refactoring", authors="Martin Fowler")
    update_item_fields(db, item_id, {"authors": "Kent Beck, Martin Fowler"})
    assert _index(db, item_id) == _expected("Kent Beck, Martin Fowler")


def test_bulk_update_reindexes_every_id(db):
    a = insert_item(db, title="A", authors="X")
    b = insert_item(db, title="B", authors="Y")
    update_items_fields(db, [a, b, 999999], {"authors": ROLES})
    assert _index(db, a) == _index(db, b) == _expected(ROLES)
    # X and Y are linked by nothing now.
    assert {r[0] for r in db.execute("SELECT name FROM authors")} == {
        "Cixin Liu", "Ken Liu"}


def test_clearing_authors_clears_the_index_and_gcs_the_orphan(db):
    item_id = insert_item(db, title="T", authors="Solo Author")
    for cleared in (None, ""):
        update_item_fields(db, item_id, {"authors": "Solo Author"})
        update_item_fields(db, item_id, {"authors": cleared})
        assert _index(db, item_id) == []
        assert db.execute(
            "SELECT COUNT(*) FROM authors WHERE name_key = 'solo author'"
        ).fetchone()[0] == 0


def test_shared_author_survives_when_one_item_drops_them(db):
    a = insert_item(db, title="A", authors="Shared, Only A")
    b = insert_item(db, title="B", authors="Shared")
    update_item_fields(db, a, {"authors": "Someone Else"})
    assert _index(db, b) == [("Shared", None, 0)]
    assert db.execute(
        "SELECT COUNT(*) FROM authors WHERE name_key = 'only a'").fetchone()[0] == 0


def test_update_without_authors_leaves_the_index_alone(db):
    item_id = insert_item(db, title="T", authors=ROLES)
    before = db.execute(
        "SELECT * FROM item_authors WHERE item_id = ?", (item_id,)).fetchall()
    update_item_fields(db, item_id, {"title": "Renamed"})
    update_items_fields(db, [item_id], {"notes": "n"})
    after = db.execute(
        "SELECT * FROM item_authors WHERE item_id = ?", (item_id,)).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_resaving_the_same_string_keeps_author_ids(db):
    item_id = insert_item(db, title="T", authors=ROLES)
    ids = _author_ids(db, item_id)
    update_item_fields(db, item_id, {"authors": ROLES})
    assert _author_ids(db, item_id) == ids


def test_identity_goes_through_normalize(db):
    a = insert_item(db, title="Hobbit", authors="J. R. R. Tolkien")
    b = insert_item(db, title="LotR", authors="J.R.R. Tolkien")
    assert _author_ids(db, a) == _author_ids(db, b)
    # The first spelling indexed is the display name.
    assert _index(db, b) == [("J. R. R. Tolkien", None, 0)]


def test_trashed_item_keeps_its_index_and_is_reindexed_on_update(db):
    item_id = insert_item(db, title="T", authors="Before")
    assert trash_item(db, item_id)
    assert _index(db, item_id) == [("Before", None, 0)]
    update_item_fields(db, item_id, {"authors": "After"})
    assert _index(db, item_id) == [("After", None, 0)]


def test_restored_twin_keeps_its_index(db):
    item_id = insert_item(
        db, title="Dune", isbn="9780441013593", authors="Frank Herbert")
    assert trash_item(db, item_id)
    again = insert_item(
        db, title="Dune (new)", isbn="9780441013593", authors="Someone Else")
    assert was_restored(again) and again == item_id
    # A restore changes no field, so the index still matches the string.
    assert _index(db, item_id) == [("Frank Herbert", None, 0)]


# -- boot-time build and parser versioning (T4) ------------------------------

from app.services import author_index  # noqa: E402

PROD_SHAPED = [
    "Cixin Liu, Ken Liu - translator",
    "William E. Shotts, Jr.",
    "Bryan O\u2019Sullivan, John Goerzen, and Donald Bruce Stewart",
    "Alberto Artasanchez & Prateek Joshi",
    "Williams, Robin",
    "Humble Book Bundle - A.I. by Packt",
    "J. R. R. Tolkien",
    "J.R.R. Tolkien",
    "Stanis\u0142aw Lem",
    "Stanislaw Lem",
    "\u5218\u6148\u6b23",
    "",
    None,
]


def _seed_unindexed(db):
    """Rows written as a pre-#117b release would have: raw INSERTs, no index,
    no recorded version. One of them trashed."""
    ids = [_insert_item(db, title=f"T{i}", isbn=None, authors=a)
           for i, a in enumerate(PROD_SHAPED)]
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (ids[0],))
    db.execute("DELETE FROM item_authors")
    db.execute("DELETE FROM authors")
    db.execute("DELETE FROM settings WHERE key = ?", (author_index.VERSION_KEY,))
    db.commit()
    return ids


def _index_keys(db, item_id):
    return [
        tuple(r) for r in db.execute(
            "SELECT a.name_key, ia.role, ia.position FROM item_authors ia "
            "JOIN authors a ON a.id = ia.author_id WHERE ia.item_id = ? "
            "ORDER BY ia.position", (item_id,))
    ]


def _expected_keys(authors):
    return [(p.name_key, p.role, p.position) for p in authors_svc.parse(authors)]


def _items_snapshot(db):
    return db.execute(
        "SELECT id, authors, updated_at FROM items ORDER BY id").fetchall()


def test_first_boot_builds_the_index_and_records_the_version(db, caplog):
    ids = _seed_unindexed(db)
    before = [tuple(r) for r in _items_snapshot(db)]
    line = author_index.rebuild_all(db)
    assert line == f"Built author index: {len(PROD_SHAPED) - 2} items, 13 authors"
    assert not caplog.records  # G3: the caller logs, after commit
    for item_id, authors in zip(ids, PROD_SHAPED):
        assert _index_keys(db, item_id) == _expected_keys(authors)
    assert db.execute(
        "SELECT value FROM settings WHERE key = ?", (author_index.VERSION_KEY,)
    ).fetchone()[0] == str(authors_svc.PARSER_VERSION)
    # The upgrade changes no item bytes.
    assert [tuple(r) for r in _items_snapshot(db)] == before
    assert not db.in_transaction


def test_second_boot_writes_nothing(db):
    _seed_unindexed(db)
    author_index.rebuild_all(db)
    changes = db.total_changes
    assert author_index.rebuild_all(db) is None
    assert db.total_changes == changes


def test_old_or_missing_version_rebuilds_and_keeps_author_ids(db):
    ids = _seed_unindexed(db)
    author_index.rebuild_all(db)
    kept = {r[0]: r[1] for r in db.execute("SELECT name_key, id FROM authors")}
    for recorded in ("0", None):
        # A stale link, as a parser change would leave behind.
        db.execute("UPDATE item_authors SET role = 'stale'")
        if recorded is None:
            db.execute("DELETE FROM settings WHERE key = ?", (author_index.VERSION_KEY,))
        else:
            db.execute("UPDATE settings SET value = ? WHERE key = ?",
                       (recorded, author_index.VERSION_KEY))
        db.commit()
        assert author_index.rebuild_all(db).startswith("Built author index")
        assert {r[0]: r[1] for r in db.execute(
            "SELECT name_key, id FROM authors")} == kept
        for item_id, authors in zip(ids, PROD_SHAPED):
            assert _index_keys(db, item_id) == _expected_keys(authors)


def test_every_boot_sweeps_orphans_left_by_a_cascade(db):
    from app.services.trash import purge_item

    item_id = insert_item(db, title="Gone", authors="Only Here")
    db.commit()
    author_index.rebuild_all(db)  # records the version
    assert trash_item(db, item_id) and purge_item(db, item_id)
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM authors WHERE name_key = 'only here'").fetchone()[0] == 1
    assert author_index.rebuild_all(db) is None
    assert db.execute(
        "SELECT COUNT(*) FROM authors WHERE name_key = 'only here'").fetchone()[0] == 0


def test_name_for(db):
    item_id = insert_item(db, title="T", authors="Ursula K. Le Guin")
    author_id = _author_ids(db, item_id)[0]
    assert author_index.name_for(db, author_id) == "Ursula K. Le Guin"
    assert author_index.name_for(db, str(author_id)) == "Ursula K. Le Guin"
    for bad in ("abc", None, "9" * 22, 999999):
        assert author_index.name_for(db, bad) is None


# -- authors_for (T7) ---------------------------------------------------------

def test_authors_for_returns_ordered_id_name_role(db):
    item_id = insert_item(db, title="Three-Body", authors=ROLES)
    ids = _author_ids(db, item_id)
    assert author_index.authors_for(db, item_id) == [
        (ids[0], "Cixin Liu", None),
        (ids[1], "Ken Liu", "translator"),
    ]


def test_authors_for_single_author(db):
    item_id = insert_item(db, title="Solo", authors="Kent Beck")
    author_id = _author_ids(db, item_id)[0]
    assert author_index.authors_for(db, item_id) == [(author_id, "Kent Beck", None)]


def test_authors_for_no_index_rows_is_empty(db):
    item_id = insert_item(db, title="No Authors")
    assert author_index.authors_for(db, item_id) == []
