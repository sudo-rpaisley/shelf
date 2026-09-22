"""The soft-delete seam: the `deleted_at` column and the `items_live` view.

This whole file pins *mechanism*, not behaviour. Nothing here — and nothing in
`app/` — ever sets `deleted_at`; deletes still hard-delete. What is pinned is
that the column arrives on both bootstrap routes and that every connection
`get_db()` hands out can read through the view, so that the plan which does
start writing a timestamp has a seam to switch on.
"""

import io
import sqlite3

import pytest

from app.database import MIGRATIONS, SCHEMA, _run_migrations, get_db, init_db


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _legacy_db(tmp_path, up_to):
    """A database as an install one version before the seam left it.

    Built from `SCHEMA` plus the numbered entries up to `up_to` — never from
    the current `MIGRATION_TABLES`, whose CREATEs run *after* the migrations
    loop and bake in columns the ALTERs add. A fixture that ran them first
    would already have every table in its final shape, so a missing or broken
    numbered entry would still leave the test green (G98).
    """
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for version, description, sql in MIGRATIONS:
        if version > up_to:
            continue
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            # Tables MIGRATION_TABLES creates do not exist on this path; those
            # migrations are baked into their CREATE TABLE anyway. Same
            # allowance tests/test_items.py::_legacy_db makes.
            pass
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (version, description),
        )
    conn.commit()
    return conn


class TestTheColumnArrivesOnTheUpgradePath:
    """Migrations 37 and 38, replayed the way a real upgrade replays them.

    Mutation-checked (G31): deleting entry 38 from `MIGRATIONS` reds
    `test_the_upgrade_adds_deleted_at_to_both_tables` on the *copies column*
    — `assert "deleted_at" in _columns(conn, "item_copies")` — and not merely
    on a missing `schema_version` row, which is G98's bar for a migration pin.
    Deleting entry 37 reds the same test on the items column.
    """

    def test_a_pre_seam_database_has_the_column_on_neither_table(self, tmp_path):
        """The fixture is genuinely pre-37, or everything below is vacuous."""
        conn = _legacy_db(tmp_path, up_to=36)
        try:
            assert "deleted_at" not in _columns(conn, "items")
            assert "deleted_at" not in _columns(conn, "item_copies")
        finally:
            conn.close()

    def test_the_upgrade_adds_deleted_at_to_both_tables(self, tmp_path):
        conn = _legacy_db(tmp_path, up_to=36)
        try:
            _run_migrations(conn)
            conn.commit()

            assert "deleted_at" in _columns(conn, "items")
            assert "deleted_at" in _columns(conn, "item_copies")

            applied = {
                r["version"] for r in conn.execute("SELECT version FROM schema_version")
            }
            assert {37, 38} <= applied
        finally:
            conn.close()

    def test_the_upgraded_column_is_null_for_every_existing_row(self, tmp_path):
        """An upgrade must not mark anybody's collection deleted."""
        conn = _legacy_db(tmp_path, up_to=36)
        try:
            cur = conn.execute("INSERT INTO items (title) VALUES ('Predates the seam')")
            item_id = cur.lastrowid
            conn.execute(
                "INSERT INTO item_copies (item_id, copy_number, is_primary) "
                "VALUES (?, 1, 1)",
                (item_id,),
            )
            conn.commit()

            _run_migrations(conn)
            conn.commit()

            assert conn.execute(
                "SELECT COUNT(*) AS c FROM items WHERE deleted_at IS NOT NULL"
            ).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM item_copies WHERE deleted_at IS NOT NULL"
            ).fetchone()["c"] == 0
        finally:
            conn.close()


def test_a_fresh_insert_leaves_deleted_at_null(db):
    """Nothing in the ordinary write path supplies the column."""
    cur = db.execute("INSERT INTO items (title) VALUES ('Still here')")
    item_id = cur.lastrowid
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)",
        (item_id,),
    )
    db.commit()

    assert db.execute(
        "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
    ).fetchone()["deleted_at"] is None
    assert db.execute(
        "SELECT deleted_at FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone()["deleted_at"] is None


class TestTheItemsLiveViewExistsOnEveryConnection:
    """`get_db()` creates the view; nothing else does.

    Mutation-checked (G31): removing the `CREATE TEMP VIEW` from `get_db()`
    reds `test_two_successive_connections_can_both_read_the_view` with
    `no such table: items_live`, on the *second* connection as well as the
    first — which is the property a module-level "we already made it" cache
    would break (G13, which is why no such cache exists).
    """

    def test_two_successive_connections_can_both_read_the_view(self, db):
        """Two connections, not one: a per-process flag would pass with one."""
        with get_db() as first:
            assert first.execute("SELECT COUNT(*) AS c FROM items_live").fetchone()["c"] == 0
        with get_db() as second:
            assert second.execute("SELECT COUNT(*) AS c FROM items_live").fetchone()["c"] == 0

    def test_the_view_is_queryable_before_the_schema_exists(self, tmp_path, monkeypatch):
        """`init_db()` opens its first connection *before* SCHEMA runs, so the
        CREATE has to succeed with no `items` table present. A view's body is
        resolved at use, not at creation, which is what makes that safe — and
        calling `init_db()` twice proves the second bootstrap is clean too.
        """
        db_path = tmp_path / "scratch" / "shelf.db"
        monkeypatch.setattr("app.config.DATABASE_PATH", db_path)
        monkeypatch.setattr("app.config.COVERS_DIR", tmp_path / "scratch" / "covers")
        monkeypatch.setattr("app.database.DATABASE_PATH", db_path)
        monkeypatch.setattr("app.database.COVERS_DIR", tmp_path / "scratch" / "covers")

        init_db()
        init_db()

        with get_db() as conn:
            assert conn.execute("SELECT COUNT(*) AS c FROM items_live").fetchone()["c"] == 0

    def test_the_view_reads_only_undeleted_rows(self, db):
        """The predicate, exercised by hand. Nothing in `app/` ever writes
        `deleted_at`, so this is the only place in the suite that does."""
        db.execute("INSERT INTO items (title) VALUES ('Visible')")
        db.execute(
            "INSERT INTO items (title, deleted_at) VALUES ('Hidden', '2026-09-18T00:00:00')"
        )
        db.commit()

        titles = {r["title"] for r in db.execute("SELECT title FROM items_live")}
        assert titles == {"Visible"}
        assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 2

    @pytest.mark.parametrize("statement", [
        "UPDATE items_live SET title = 'nope'",
        "INSERT INTO items_live (title) VALUES ('nope')",
        "DELETE FROM items_live",
    ])
    def test_writing_through_the_view_fails_loudly(self, statement, db):
        """The runtime backstop behind the lint: a write that reaches the view
        by mistake raises instead of silently hitting a filtered row set."""
        with pytest.raises(sqlite3.OperationalError, match="cannot modify items_live"):
            db.execute(statement)

    def test_the_view_is_temp_and_therefore_not_in_the_persistent_schema(self, db):
        """TEMP is the whole reason backups stay restorable — see the backup
        round-trip below for the end-to-end version."""
        assert db.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type = 'view'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM sqlite_temp_master "
            "WHERE type = 'view' AND name = 'items_live'"
        ).fetchone()["c"] == 1

    def test_select_star_picks_up_a_column_added_after_the_view(self, db):
        """`SELECT *` rather than a frozen column list, on the connection that
        matters: a future `items` migration needs no change in `get_db()`."""
        before = {d[0] for d in db.execute("SELECT * FROM items_live").description}
        assert "_probe_col" not in before

        db.execute("ALTER TABLE items ADD COLUMN _probe_col TEXT")
        after = {d[0] for d in db.execute("SELECT * FROM items_live").description}
        assert "_probe_col" in after

    def test_get_db_yields_outside_a_transaction(self, db):
        """`CREATE TEMP VIEW` is DDL, and under sqlite3's legacy transaction
        control DDL opens no implicit transaction (G16) — so adding it to
        `get_db()` leaves the commit-on-exit semantics exactly as they were.
        """
        with get_db() as conn:
            assert conn.in_transaction is False


class TestTheCopiesLiveViewExistsOnEveryConnection:
    """The second seam, for `item_copies`. `get_db()` creates it; nothing else.

    Mutation-checked (G31): removing the `CREATE TEMP VIEW` for `copies_live`
    from `get_db()` reds `test_two_successive_connections_can_both_read_the_view`
    with `no such table: copies_live`, on the second connection as well as the
    first. Dropping `AND i.deleted_at IS NULL` from the view's body reds
    `test_the_view_hides_every_copy_of_a_trashed_item` and nothing else, which
    is what makes that clause's own pin non-vacuous. Dropping `TEMP` reds
    `test_the_view_is_temp_and_therefore_not_in_the_persistent_schema` and both
    tests in `TestBackupsStayRestorableWithTheViewInPlace`.
    """

    def _seed_item_with_copy(self, db, title, copy_number=1):
        cur = db.execute("INSERT INTO items (title) VALUES (?)", (title,))
        item_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO item_copies (item_id, copy_number, is_primary) "
            "VALUES (?, ?, 1)",
            (item_id, copy_number),
        )
        return item_id, cur.lastrowid

    def test_two_successive_connections_can_both_read_the_view(self, db):
        """Two connections, not one: a per-process flag would pass with one."""
        with get_db() as first:
            assert first.execute(
                "SELECT COUNT(*) AS c FROM copies_live"
            ).fetchone()["c"] == 0
        with get_db() as second:
            assert second.execute(
                "SELECT COUNT(*) AS c FROM copies_live"
            ).fetchone()["c"] == 0

    def test_the_view_is_queryable_before_the_schema_exists(self, tmp_path, monkeypatch):
        """Same bootstrap ordering as `items_live`: `init_db()` opens its first
        connection before SCHEMA runs, so neither `item_copies` nor `items`
        exists when this CREATE executes. A view's body is resolved at use.
        """
        db_path = tmp_path / "scratch" / "shelf.db"
        monkeypatch.setattr("app.config.DATABASE_PATH", db_path)
        monkeypatch.setattr("app.config.COVERS_DIR", tmp_path / "scratch" / "covers")
        monkeypatch.setattr("app.database.DATABASE_PATH", db_path)
        monkeypatch.setattr("app.database.COVERS_DIR", tmp_path / "scratch" / "covers")

        init_db()
        init_db()

        with get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM copies_live"
            ).fetchone()["c"] == 0

    def test_the_view_hides_a_copy_whose_own_column_is_set(self, db):
        """The first half of the predicate. Nothing in `app/` writes
        `deleted_at`, so this is the only place in the suite that does."""
        item_id, live_copy = self._seed_item_with_copy(db, "Two copies")
        cur = db.execute(
            "INSERT INTO item_copies (item_id, copy_number, is_primary, deleted_at) "
            "VALUES (?, 2, 0, '2026-09-18T00:00:00')",
            (item_id,),
        )
        trashed_copy = cur.lastrowid
        db.commit()

        visible = {r["id"] for r in db.execute("SELECT id FROM copies_live")}
        assert visible == {live_copy}
        assert trashed_copy not in visible
        assert db.execute(
            "SELECT COUNT(*) AS c FROM item_copies"
        ).fetchone()["c"] == 2

    def test_the_view_hides_every_copy_of_a_trashed_item(self, db):
        """The join is the design decision: trashing an item needs no write to
        its copies, so their own column stays NULL and they still vanish."""
        live_item, live_copy = self._seed_item_with_copy(db, "Kept")
        trashed_item, orphan_copy = self._seed_item_with_copy(db, "Trashed")
        db.execute(
            "UPDATE items SET deleted_at = '2026-09-18T00:00:00' WHERE id = ?",
            (trashed_item,),
        )
        db.commit()

        visible = {r["id"] for r in db.execute("SELECT id FROM copies_live")}
        assert visible == {live_copy}
        assert orphan_copy not in visible
        # The copy itself was never stamped — the join is what hid it.
        assert db.execute(
            "SELECT deleted_at FROM item_copies WHERE id = ?", (orphan_copy,)
        ).fetchone()["deleted_at"] is None

    @pytest.mark.parametrize("statement", [
        "UPDATE copies_live SET copy_number = 9",
        "INSERT INTO copies_live (item_id, copy_number) VALUES (1, 1)",
        "DELETE FROM copies_live",
    ])
    def test_writing_through_the_view_fails_loudly(self, statement, db):
        """The runtime backstop behind the lint, same as `items_live`."""
        with pytest.raises(sqlite3.OperationalError, match="cannot modify copies_live"):
            db.execute(statement)

    def test_the_view_is_temp_and_therefore_not_in_the_persistent_schema(self, db):
        """TEMP is why backups stay restorable — see the round-trip below."""
        assert db.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type = 'view'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM sqlite_temp_master "
            "WHERE type = 'view' AND name = 'copies_live'"
        ).fetchone()["c"] == 1

    def test_select_star_picks_up_a_column_added_after_the_view(self, db):
        """`c.*` rather than a frozen column list: a future `item_copies`
        migration needs no change in `get_db()`."""
        before = {d[0] for d in db.execute("SELECT * FROM copies_live").description}
        assert "_probe_copy_col" not in before

        db.execute("ALTER TABLE item_copies ADD COLUMN _probe_copy_col TEXT")
        after = {d[0] for d in db.execute("SELECT * FROM copies_live").description}
        assert "_probe_copy_col" in after


class TestBackupsStayRestorableWithTheViewInPlace:
    """The regression the TEMP choice exists to prevent, end to end through
    the real validator rather than by inspecting the schema.

    Both tests assert **zero** persistent views of any name, so they cover
    `copies_live` with no edit — adding a second TEMP view changes neither
    assertion, and making either view persistent reds both.
    """

    def _align_paths(self, monkeypatch):
        import app.config as config
        monkeypatch.setattr("app.routers.settings.DATA_DIR", config.DATA_DIR)
        monkeypatch.setattr("app.routers.settings.DATABASE_PATH", config.DATABASE_PATH)

    def test_a_backup_taken_on_this_schema_carries_no_views(
        self, admin_client, tmp_path, monkeypatch
    ):
        self._align_paths(monkeypatch)
        backup = admin_client.get("/api/settings/backup")
        assert backup.status_code == 200

        p = tmp_path / "backup.db"
        p.write_bytes(backup.content)
        raw = sqlite3.connect(str(p))
        try:
            assert raw.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'view'"
            ).fetchone()[0] == 0
        finally:
            raw.close()

    def test_that_same_backup_restores(self, admin_client, tmp_path, monkeypatch):
        """A persistent `items_live` would land in the VACUUM INTO copy and
        this POST would answer `Database contains views — not allowed`."""
        self._align_paths(monkeypatch)
        backup = admin_client.get("/api/settings/backup")
        assert backup.status_code == 200

        resp = admin_client.post(
            "/api/settings/restore",
            files={
                "file": (
                    "backup.db",
                    io.BytesIO(backup.content),
                    "application/octet-stream",
                )
            },
        )
        data = resp.json()
        assert data["ok"] is True, data
