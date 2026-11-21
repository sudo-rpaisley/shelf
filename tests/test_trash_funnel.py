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

import re

import pytest

from app.services import item_copies, item_merge, item_write, location_tree
from app.services.item_write import insert_item

#: `datetime('now')`'s own shape — used to pin the None-default case without
#: coupling to the exact second it ran.
_NOW_SHAPED = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


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

    Normal acquisition now deliberately creates a first copy even unlocated;
    unlike RAW fixture inserts, reads/ensure never create one. Still assert
    the actual identity, so trashing copy None cannot satisfy a negative pin.
    """
    item_id = _item(db, location_id=location_id)
    primary = _primary_id(db, item_id)
    if primary is None:
        primary = item_copies.add_copy(db, item_id, copy_fields or None)
    assert primary is not None
    return item_id, primary


def _assert_last_owned_refusal(db, item_id, copy_id, **kwargs):
    """Ordinary last-owned removal (including explicit `at`) remains forbidden."""
    before_item = tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    before_copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")]
    with pytest.raises(ValueError, match="An owned physical item must keep at least one copy"):
        item_copies.trash_copy(db, copy_id, **kwargs)
    assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()) == before_item
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")] == before_copies


def _replay_upstream_all_trashed(db, item_id, copy_id):
    """Replay the captured U7e0369e7 all-trashed history, not an HTTP removal.

    Immutable parents/upstream/app/services/item_copies.py:493-556 permitted
    the last copy to be stamped/demoted and its seam cleared (451-490). The
    native archive replays precisely this source history with replay=True in
    app/services/archive.py::_import_copies. No lineage marker/ACL is removed,
    no route accepts a bypass, and the original copy ID/number/barcode survive.
    """
    assert [row[0] for row in db.execute("SELECT id FROM item_copies WHERE item_id = ?", (item_id,))] == [copy_id]
    before = dict(_copy_row(db, copy_id))
    result = item_copies.trash_copy(db, copy_id, at="2026-01-02 03:04:05", replay=True)
    after = _copy_row(db, copy_id)
    for key in ("id", "item_id", "copy_number", "copy_barcode", "location_id", "position_order", "created_at"):
        assert after[key] == before[key]
    assert after["deleted_at"] == "2026-01-02 03:04:05" and after["is_primary"] == 0
    return result


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
        living_node = location_tree.legacy_location_to_node(db, living, create=True)
        second = item_copies.add_copy(db, item_id, {"location_id": living_node})
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
        living_node = location_tree.legacy_location_to_node(db, living, create=True)
        second = item_copies.add_copy(db, item_id, {"location_id": living_node})
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

    def test_an_explicit_at_is_stored_verbatim(self, db):
        item_id = _item(db)

        assert item_write.trash_item(db, item_id, at="2026-01-02 03:04:05") is True

        assert db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"] == "2026-01-02 03:04:05"

    def test_no_at_stores_a_datetime_now_shaped_value(self, db):
        item_id = _item(db)

        assert item_write.trash_item(db, item_id) is True

        stamp = db.execute(
            "SELECT deleted_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()["deleted_at"]
        assert _NOW_SHAPED.match(stamp), stamp


class TestTrashCopy:
    def test_trashing_a_secondary_touches_nothing_else(self, db):
        living = _location(db)
        item_id = _item(db, location_id=living)
        living_node = location_tree.legacy_location_to_node(db, living, create=True)
        second = item_copies.add_copy(db, item_id, {"location_id": living_node})
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
        db.execute("INSERT INTO location_nodes (id, name) VALUES (100, 'Unmapped ID control')")
        item_id = _item(db, location_id=living)
        primary = _primary_id(db, item_id)
        study_node = location_tree.legacy_location_to_node(db, study, create=True)
        assert study_node is not None and study_node != study
        second = item_copies.add_copy(db, item_id, {"location_id": study_node})
        third = item_copies.add_copy(db, item_id, {"location_id": study_node})
        item_copies.update_copy(db, second, {"position_order": 7})

        result = item_copies.trash_copy(db, primary)

        assert result["was_primary"] is True
        assert result["promoted_copy_id"] == second
        assert result["remaining"] == 2
        assert _primary_id(db, item_id) == second
        # The seam follows the promoted survivor.
        assert _seam(db, item_id) == study
        assert _live_copy(db, second)["location_id"] == study_node
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

        _assert_last_owned_refusal(db, item_id, only)
        result = _replay_upstream_all_trashed(db, item_id, only)

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
        _assert_last_owned_refusal(db, item_id, only)
        survivor = item_copies.add_copy(db, item_id)

        item_copies.trash_copy(db, only)

        row = _copy_row(db, only)
        assert row["deleted_at"] is not None
        assert row["is_primary"] == 0
        assert _primary_id(db, item_id) == survivor

    def test_a_second_call_returns_none(self, db):
        item_id, only = _item_with_copy(db)
        _assert_last_owned_refusal(db, item_id, only)
        item_copies.add_copy(db, item_id)
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

    def test_an_explicit_at_is_stored_verbatim(self, db):
        item_id, only = _item_with_copy(db)
        _assert_last_owned_refusal(db, item_id, only, at="2026-01-02 03:04:05")
        item_copies.add_copy(db, item_id)

        result = item_copies.trash_copy(db, only, at="2026-01-02 03:04:05")

        assert result is not None
        assert _copy_row(db, only)["deleted_at"] == "2026-01-02 03:04:05"

    def test_no_at_stores_a_datetime_now_shaped_value(self, db):
        item_id, only = _item_with_copy(db)
        _assert_last_owned_refusal(db, item_id, only)
        item_copies.add_copy(db, item_id)

        result = item_copies.trash_copy(db, only)

        assert result is not None
        stamp = _copy_row(db, only)["deleted_at"]
        assert _NOW_SHAPED.match(stamp), stamp


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

        _assert_last_owned_refusal(db, item_id, only)
        _replay_upstream_all_trashed(db, item_id, only)
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
        living_node = location_tree.legacy_location_to_node(db, living, create=True)
        second = item_copies.add_copy(db, item_id, {"location_id": living_node})

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

        _assert_last_owned_refusal(db, item_id, only)
        _replay_upstream_all_trashed(db, item_id, only)
        assert _seam(db, item_id) is None

        result = item_copies.restore_copy(db, only)

        assert result["is_primary"] is True
        assert _primary_id(db, item_id) == only
        assert _seam(db, item_id) == study

    def test_restoring_an_unlocated_copy_promotes_without_inventing_a_seam(self, db):
        item_id, only = _item_with_copy(db)
        _assert_last_owned_refusal(db, item_id, only)
        _replay_upstream_all_trashed(db, item_id, only)

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
    def test_only_the_named_soft_delete_sites_put_anything_into_trash(self):
        """**Exactly six entry points use the trashing funnels**:
        direction** — the item delete (`items.py`), the Audiobookshelf
        excluded-library cleanup (`sync.py`), Remove copy (`item_copies.py`)
        and the CSV importer's own-file-says-deleted row (`items_csv.py`,
        soft-delete-export T6 — a row with no database hit and the file's
        `deleted` column set lands straight in Trash, never as a live row
        that a second pass would have to delete again).

        This test's earlier form said *no* route could; soft-delete-trash T4
        flipped the delete sites and narrowed it to three. Native Komga and
        RomM excluded-scope cleanup are the two approved additions. RomM's
        route delegates to exactly one records-service cleanup; no module-wide
        writer exemption or permission to hard-delete is implied.
        """
        import ast
        from pathlib import Path

        # `ast`, not a text scan: prose naming the functions is not a call
        # (G53's shape).
        names = {"trash_item", "trash_copy"}
        routers = Path(__file__).resolve().parents[1] / "app" / "routers"
        callers = set()
        call_sites = []
        paths = [*routers.rglob("*.py"), routers.parent / "services" / "romm_records.py"]
        for path in paths:
            tree = ast.parse(path.read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
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
                    owner = parents[node]
                    while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                        owner = parents[owner]
                    site = (path.relative_to(routers.parent).as_posix(), getattr(owner, "name", "<module>"), called)
                    callers.add(site)
                    call_sites.append(site)
        assert callers == {
            ("routers/items.py", "delete_item", "trash_item"),
            ("routers/sync.py", "cleanup_excluded_libraries", "trash_item"),
            ("routers/item_copies.py", "remove_copy", "trash_copy"),
            ("routers/items_csv.py", "import_csv", "trash_item"),
            ("routers/komga.py", "cleanup_libraries", "trash_item"),
            ("services/romm_records.py", "cleanup_excluded_platforms", "trash_item"),
        }, f"the routes that put rows into Trash changed: {sorted(callers)}"
        assert len(call_sites) == len(callers) == 6, "each approved cleanup/deletion has exactly one funnel call site"
        romm = ast.parse((routers / "romm.py").read_text())
        cleanup = next(node for node in romm.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "cleanup_platforms")
        delegates = [node for node in ast.walk(cleanup) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                     and node.func.value.id == "romm_records" and node.func.attr == "cleanup_excluded_platforms"]
        assert len(delegates) == 1

        # Bind the exact restore seams, not whole modules. Retained legacy-book
        # identities add two guarded native-funnel callers to the original five
        # modules; that is not permission for arbitrary route-side restoration.
        restore_sites = []
        for path in routers.rglob("*.py"):
            tree = ast.parse(path.read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None
                )
                if called in {"restore_item", "restore_copy"}:
                    owner = parents[node]
                    while not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                        owner = parents[owner]
                    restore_sites.append((path.name, getattr(owner, "name", "<module>"), called))
        assert set(restore_sites) == {
            ("music.py", "add_music_release", "restore_item"),
            ("periodicals.py", "confirm_periodical_issue", "restore_item"),
            ("items_csv.py", "import_csv", "restore_item"),
            ("hardcover.py", "add_hardcover_to_shelf", "restore_item"),
            ("trash.py", "restore_item", "restore_item"),
            ("trash.py", "restore_copy", "restore_copy"),
            ("items_common.py", "_save_item", "restore_item"),
            ("item_barcode_edit.py", "restore_legacy_book", "restore_item"),
        }, f"the exact guarded native restore call sites changed: {sorted(restore_sites)}"
        assert len(restore_sites) == 8, "each approved restore seam has exactly one funnel call"
