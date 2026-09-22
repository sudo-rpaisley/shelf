"""The four trash funnel functions, proven by calling them directly.

`item_write.trash_item` / `restore_item` and `item_copies.trash_copy` /
`restore_copy` are the only writers of `deleted_at`. They landed ahead of the
flip, called by no route, so the collision rules could be shown to hold before
any route could produce a trashed row. Since soft-delete-trash T4 the three
soft delete sites call the trashing pair and the Trash page restores; this file
still proves every rule at the function, which is the faster and more explicit
seed.

The source pin that holds the four-function claim lives in
`tests/test_item_write.py::TestSingleWritePath`.
"""

import pytest

from app.services import item_copies, item_merge, item_write
from app.services.item_write import insert_item


def _location(db, name="Living Room"):
    return db.execute("INSERT INTO locations (name) VALUES (?)", (name,)).lastrowid


def _item(db, title="Trash Test", *, location_id=None):
    return insert_item(db, title=title, source="test", location_id=location_id)


def _copy_row(db, copy_id):
    """The physical copy row — this file has to see what the view hides."""
    return db.execute(
        "SELECT * FROM item_copies WHERE id = ?", (copy_id,)
    ).fetchone()


def _live_copy(db, copy_id):
    return db.execute(
        "SELECT * FROM copies_live WHERE id = ?", (copy_id,)
    ).fetchone()


def _seam(db, item_id):
    return db.execute(
        "SELECT location_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()["location_id"]


def _primary_id(db, item_id):
    row = db.execute(
        "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1",
        (item_id,),
    ).fetchone()
    return row["id"] if row else None


def _item_with_copy(db, *, location_id=None, **copy_fields):
    """An item **and** its first, primary copy — returned as a pair.

    `insert_item` mints a primary copy only when the item carries a location
    (`sync_primary_location` never invents one merely to store a NULL), so an
    unlocated item has **no copies at all**. Reading `_primary_id` on one
    returns `None`, and a pin built on that silently tests nothing: caught
    here when `TestTheGeminiN1Contract` passed while trashing copy `None`,
    which is the exact shape G31 warns about — an assertion that cannot fail.
    Every test below that needs a copy goes through this helper.
    """
    item_id = _item(db, location_id=location_id)
    primary = _primary_id(db, item_id)
    if primary is None:
        primary = item_copies.add_copy(db, item_id, copy_fields or None)
    assert primary is not None
    return item_id, primary


class TestTrashItem:
    def test_trashing_hides_the_row_and_reports_that_it_moved(self, db):
        item_id = _item(db)
        assert item_write.trash_item(db, item_id) is True
        assert db.execute(
            "SELECT 1 FROM items_live WHERE id = ?", (item_id,)
        ).fetchone() is None
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] is not None

    def test_a_second_call_writes_nothing_and_says_so(self, db):
        item_id = _item(db)
        item_write.trash_item(db, item_id)
        stamp = db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"]

        assert item_write.trash_item(db, item_id) is False
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] == stamp

    def test_an_unknown_id_is_false_not_an_error(self, db):
        assert item_write.trash_item(db, 99999) is False

    def test_restoring_brings_it_back_and_reports_that_it_moved(self, db):
        item_id = _item(db)
        item_write.trash_item(db, item_id)

        assert item_write.restore_item(db, item_id) is True
        assert db.execute(
            "SELECT 1 FROM items_live WHERE id = ?", (item_id,)
        ).fetchone() is not None
        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] is None

    def test_restoring_a_live_row_writes_nothing_and_says_so(self, db):
        item_id = _item(db)
        assert item_write.restore_item(db, item_id) is False

    def test_trashing_an_item_hides_its_copies_with_their_own_column_null(self, db):
        """`copies_live` joins the items relation, so trashing an item takes
        its copies out of view with **no write to `item_copies` at all** —
        which is what lets them come back intact on restore."""
        living = _location(db)
        item_id = _item(db, location_id=living)
        second = item_copies.add_copy(db, item_id, {"location_id": living})
        primary = _primary_id(db, item_id)

        item_write.trash_item(db, item_id)

        assert _live_copy(db, primary) is None
        assert _live_copy(db, second) is None
        # Their own column was never touched.
        assert _copy_row(db, primary)["deleted_at"] is None
        assert _copy_row(db, second)["deleted_at"] is None
        assert _copy_row(db, primary)["is_primary"] == 1

    def test_restoring_an_item_returns_its_copies_with_the_primary_intact(self, db):
        living = _location(db)
        item_id = _item(db, location_id=living)
        second = item_copies.add_copy(db, item_id, {"location_id": living})
        primary = _primary_id(db, item_id)

        item_write.trash_item(db, item_id)
        item_write.restore_item(db, item_id)

        assert _live_copy(db, primary) is not None
        assert _live_copy(db, second) is not None
        assert _primary_id(db, item_id) == primary

    def test_trashing_leaves_the_scan_log_and_wishlist_membership_alone(self, db):
        from app.services import lists

        item_id = insert_item(db, title="Wanted", source="test", owned=0,
                              wishlisted=True)
        db.execute(
            "INSERT INTO scan_log (isbn, result, item_id) VALUES (?, 'added', ?)",
            ("9780000000118", item_id),
        )

        item_write.trash_item(db, item_id)

        assert lists.is_member(db, lists.WISHLIST, item_id)
        assert db.execute(
            "SELECT COUNT(*) AS n FROM scan_log WHERE item_id = ?", (item_id,)
        ).fetchone()["n"] == 1

    def test_a_restore_cannot_collide_because_the_slot_was_never_given_up(self, db):
        """A trashed row keeps its `UNIQUE(isbn, media_type)` slot, so nothing
        can have taken it meanwhile — which is why `restore_item` needs no
        collision handling of its own.

        Asserted against the **constraint**, with a raw insert that goes round
        the funnel deliberately. Going through `insert_item` would prove
        nothing here: the funnel's own collision rule now restores such a row
        rather than colliding with it, so the pin would be measuring the
        funnel's behaviour instead of the database's.
        """
        import sqlite3

        item_id = insert_item(db, title="Held", source="test",
                              isbn="9780000000118", media_type="book")
        item_write.trash_item(db, item_id)

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO items (title, source, isbn, media_type) "
                "VALUES ('Rival', 'test', '9780000000118', 'book')"
            )

        assert item_write.restore_item(db, item_id) is True


class TestTrashCopy:
    def test_trashing_a_secondary_touches_nothing_else(self, db):
        living = _location(db)
        item_id = _item(db, location_id=living)
        second = item_copies.add_copy(db, item_id, {"location_id": living})
        primary = _primary_id(db, item_id)

        result = item_copies.trash_copy(db, second)

        assert result["was_primary"] is False
        assert result["promoted_copy_id"] is None
        assert result["remaining"] == 1
        assert _live_copy(db, second) is None
        assert _primary_id(db, item_id) == primary
        assert _seam(db, item_id) == living

    def test_trashing_the_primary_promotes_the_lowest_survivor_and_keeps_position(
        self, db
    ):
        living = _location(db)
        study = _location(db, "Study")
        item_id = _item(db, location_id=living)
        primary = _primary_id(db, item_id)
        second = item_copies.add_copy(db, item_id, {"location_id": study})
        third = item_copies.add_copy(db, item_id, {"location_id": study})
        item_copies.update_copy(db, second, {"position_order": 7})

        result = item_copies.trash_copy(db, primary)

        assert result["was_primary"] is True
        assert result["promoted_copy_id"] == second
        assert result["remaining"] == 2
        assert _primary_id(db, item_id) == second
        # The seam follows the promoted survivor.
        assert _seam(db, item_id) == study
        # And the survivor keeps its shelf position — no location_id in the
        # promotion's fields, so the funnel's clearing rule does not fire.
        assert _live_copy(db, second)["position_order"] == 7
        assert _live_copy(db, third) is not None

    def test_trashing_the_last_copy_nulls_the_seam_and_invents_nothing(self, db):
        """G86: a located item with no copies is a legitimate state, and the
        seam write must not re-enter `sync_primary_location` in a way that
        mints a replacement (G96's ordering)."""
        living = _location(db)
        item_id = _item(db, location_id=living)
        only = _primary_id(db, item_id)

        result = item_copies.trash_copy(db, only)

        assert result["remaining"] == 0
        assert _seam(db, item_id) is None
        assert db.execute(
            "SELECT COUNT(*) AS n FROM copies_live WHERE item_id = ?", (item_id,)
        ).fetchone()["n"] == 0
        # No copy was invented to hold the null.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM item_copies WHERE item_id = ?", (item_id,)
        ).fetchone()["n"] == 1

    def test_trashing_demotes_in_the_same_statement_that_stamps(self, db):
        item_id, only = _item_with_copy(db)

        item_copies.trash_copy(db, only)

        row = _copy_row(db, only)
        assert row["deleted_at"] is not None
        assert row["is_primary"] == 0

    def test_a_second_call_returns_none(self, db):
        _item_id, only = _item_with_copy(db)
        assert item_copies.trash_copy(db, only) is not None
        assert item_copies.trash_copy(db, only) is None

    def test_an_unknown_copy_id_returns_none(self, db):
        assert item_copies.trash_copy(db, 99999) is None

    def test_a_trashed_copy_keeps_its_number_and_barcode_slots(self, db):
        item_id, _primary = _item_with_copy(db)
        second = item_copies.add_copy(db, item_id, {"copy_barcode": "BC-1"})

        item_copies.trash_copy(db, second)

        row = _copy_row(db, second)
        assert row["copy_number"] == 2
        assert row["copy_barcode"] == "BC-1"
        # The next copy numbers above it rather than reusing 2.
        third = item_copies.add_copy(db, item_id)
        assert _copy_row(db, third)["copy_number"] == 3


class TestTheGeminiN1Contract:
    """`gemini-N1`, reproduced live 2026-09-18 and closed by the demote.

    `add_copy` decides primary from a `copies_live` census, while
    `idx_item_copies_one_primary` is a partial unique index over the
    **physical** table that does not exclude trashed rows. Without
    `trash_copy`'s demote: trash an item's only copy while it is primary, add
    another, and a census of zero makes the new copy primary — colliding with
    the trashed row still holding the slot.

    **`add_copy` is unchanged**, deliberately. Its own comment already reasons
    against this contract; the demote is what makes that reasoning true.
    """

    def test_trashing_the_only_primary_then_adding_a_copy_succeeds(self, db):
        item_id, only = _item_with_copy(db)
        assert _copy_row(db, only)["is_primary"] == 1

        item_copies.trash_copy(db, only)
        fresh = item_copies.add_copy(db, item_id)

        assert _copy_row(db, fresh)["is_primary"] == 1
        assert _primary_id(db, item_id) == fresh
        # Exactly one physical primary — the index's own invariant.
        assert db.execute(
            "SELECT COUNT(*) AS n FROM item_copies "
            "WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()["n"] == 1


class TestRestoreCopy:
    def test_a_restored_copy_returns_as_a_secondary(self, db):
        living = _location(db)
        item_id = _item(db, location_id=living)
        primary = _primary_id(db, item_id)
        second = item_copies.add_copy(db, item_id, {"location_id": living})

        item_copies.trash_copy(db, second)
        result = item_copies.restore_copy(db, second)

        assert result["is_primary"] is False
        assert _live_copy(db, second) is not None
        assert _primary_id(db, item_id) == primary
        assert result["remaining"] == 2

    def test_restoring_onto_an_item_with_no_primary_promotes_and_moves_the_seam(
        self, db
    ):
        study = _location(db, "Study")
        item_id = _item(db, location_id=study)
        only = _primary_id(db, item_id)

        item_copies.trash_copy(db, only)
        assert _seam(db, item_id) is None

        result = item_copies.restore_copy(db, only)

        assert result["is_primary"] is True
        assert _primary_id(db, item_id) == only
        assert _seam(db, item_id) == study

    def test_restoring_an_unlocated_copy_promotes_without_inventing_a_seam(self, db):
        item_id, only = _item_with_copy(db)
        item_copies.trash_copy(db, only)

        result = item_copies.restore_copy(db, only)

        assert result["is_primary"] is True
        assert _seam(db, item_id) is None

    def test_restoring_a_copy_of_a_trashed_item_is_refused(self, db):
        """A refusal, not a restore: the copy is already invisible by way of
        its item, so clearing its own column would change nothing a reader
        can see. Restore the item instead."""
        item_id, _primary = _item_with_copy(db)
        second = item_copies.add_copy(db, item_id)
        item_copies.trash_copy(db, second)
        item_write.trash_item(db, item_id)

        assert item_copies.restore_copy(db, second) is None
        # And nothing was written.
        assert _copy_row(db, second)["deleted_at"] is not None

    def test_restoring_a_live_copy_returns_none(self, db):
        _item_id, only = _item_with_copy(db)
        assert item_copies.restore_copy(db, only) is None

    def test_an_unknown_copy_id_returns_none(self, db):
        assert item_copies.restore_copy(db, 99999) is None

    def test_a_restore_cannot_collide_on_the_number_it_kept(self, db):
        item_id, _first = _item_with_copy(db)
        second = item_copies.add_copy(db, item_id)
        item_copies.trash_copy(db, second)
        third = item_copies.add_copy(db, item_id)

        assert item_copies.restore_copy(db, second) is not None
        numbers = sorted(
            r["copy_number"] for r in db.execute(
                "SELECT copy_number FROM copies_live WHERE item_id = ?", (item_id,)
            ).fetchall()
        )
        assert numbers == [1, 2, 3]
        assert _copy_row(db, third)["copy_number"] == 3


class TestMergeReparentsTrashedCopies:
    """gemini-M1 (design plan §4): `_reparent_copies` used to select the
    losing item's copies from `copies_live`, so a trashed copy was invisible
    to it, stayed parented to the husk, and was destroyed by the merge
    route's cascading `DELETE FROM items` two lines later — a restorable row
    lost for good. Exercised at the service level, the way the merge route
    itself calls it (`app/routers/items.py`): `reparent_children` then the
    `DELETE`."""

    def _merge_fixture(self, db):
        """A keeper with two live copies, and a husk with one live copy and
        one trashed copy — trashed through the funnel, never by hand SQL."""
        keep_id, keep_primary = _item_with_copy(db, location_id=_location(db, "Living Room"))
        item_copies.add_copy(db, keep_id)

        husk_id, husk_primary = _item_with_copy(db, location_id=_location(db, "Study"))
        husk_trashed = item_copies.add_copy(db, husk_id)
        item_copies.trash_copy(db, husk_trashed)

        return keep_id, keep_primary, husk_id, husk_trashed

    def test_the_trashed_copy_moves_to_the_keeper_still_trashed_and_restorable(self, db):
        keep_id, keep_primary, husk_id, husk_trashed = self._merge_fixture(db)
        keep_highest = db.execute(
            "SELECT COALESCE(MAX(copy_number), 0) AS n FROM item_copies WHERE item_id = ?",
            (keep_id,),
        ).fetchone()["n"]

        item_merge.reparent_children(db, keep_id, husk_id)
        db.execute("DELETE FROM items WHERE id = ?", (husk_id,))

        moved = _copy_row(db, husk_trashed)
        # Existence first (G31: assert aborts, so order decides what a
        # mutation teaches). Under the defect the row is never reparented,
        # stays on the husk, and the caller's cascading DELETE destroys it
        # outright — so `moved` would be None here, the clearest possible
        # signal of the bug this pin exists to catch.
        assert moved is not None
        assert moved["deleted_at"] is not None
        assert item_copies.restore_copy(db, husk_trashed) is not None
        assert moved["item_id"] == keep_id
        assert moved["copy_number"] > keep_highest
        # The keeper's own primary is untouched by the move.
        assert _primary_id(db, keep_id) == keep_primary

    def test_no_item_copies_row_references_the_husk_after_the_merge(self, db):
        keep_id, _keep_primary, husk_id, _husk_trashed = self._merge_fixture(db)
        total_before = db.execute(
            "SELECT COUNT(*) AS n FROM item_copies"
        ).fetchone()["n"]

        item_merge.reparent_children(db, keep_id, husk_id)
        db.execute("DELETE FROM items WHERE id = ?", (husk_id,))

        # Row count first. "No row still points at the husk" is true both
        # when a copy is correctly reparented AND when it is destroyed by
        # the cascade — checked alone, it survives the very mutation this
        # pin exists to catch (verified: this file's mutation procedure
        # found it green on the first attempt, diagnosed per G108). Row
        # conservation is what actually distinguishes the two: reparenting
        # moves rows, the defect deletes one along with the husk.
        total_after = db.execute(
            "SELECT COUNT(*) AS n FROM item_copies"
        ).fetchone()["n"]
        assert total_after == total_before
        # The physical table, on purpose: this is the negative this task
        # asks for, and it is outside the items_live lint's scope (app/
        # only).
        assert db.execute(
            "SELECT COUNT(*) AS n FROM item_copies WHERE item_id = ?", (husk_id,)
        ).fetchone()["n"] == 0


class TestNoRouteCallsThem:
    def test_only_the_three_soft_delete_sites_put_anything_into_trash(self):
        """**Exactly three routes write `deleted_at` in the trashing
        direction** — the item delete (`items.py`), the Audiobookshelf
        excluded-library cleanup (`sync.py`) and Remove copy
        (`item_copies.py`).

        This test's earlier form said *no* route could; soft-delete-trash T4
        flipped the delete sites and narrowed it to the named three. A fourth
        caller is a new way into Trash and should be a decision, not a
        drive-by.
        """
        import ast
        from pathlib import Path

        # `ast`, not a text scan: prose naming the functions is not a call
        # (G53's shape).
        names = {"trash_item", "trash_copy"}
        routers = Path(__file__).resolve().parents[1] / "app" / "routers"
        callers = set()
        for path in routers.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else None
                )
                if called in names:
                    callers.add((path.name, called))
        assert callers == {
            ("items.py", "trash_item"),
            ("sync.py", "trash_item"),
            ("item_copies.py", "trash_copy"),
        }, f"the routes that put rows into Trash changed: {sorted(callers)}"

        # And the restore side is exactly these five routers, no more.
        restorers = set()
        for path in routers.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None
                )
                if called in {"restore_item", "restore_copy"}:
                    restorers.add(path.name)
        # trash.py is Trash's own Restore buttons (soft-delete-trash T2).
        assert restorers == {"music.py", "periodicals.py", "items_csv.py",
                             "hardcover.py", "trash.py"}, (
            "the direct restores are music's, periodicals', items_csv's, "
            "Hardcover add-to-shelf's and Trash's own — every other add path restores "
            f"through insert_item's funnel, found: {sorted(restorers)}"
        )
